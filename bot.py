import asyncio
import logging
import feedparser
import json
import os
import re
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# Импортируем видео-генератор (файл video_bot.py должен быть рядом)
try:
    from video_bot import send_video_to_admin
    VIDEO_ENABLED = True
except ImportError:
    VIDEO_ENABLED = False
    logger.warning("video_bot.py не найден — генерация видео отключена.")

# ─── Конфиг ──────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
CHANNEL_ID  = os.getenv("CHANNEL_ID", "@ваш_канал")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0"))

# Сколько постов отправлять на модерацию каждый час
POSTS_PER_HOUR = int(os.getenv("POSTS_PER_HOUR", "2"))

# Рабочие часы по Киеву (Europe/Kyiv)
KYIV_TZ    = ZoneInfo("Europe/Kyiv")
HOUR_START = 8   # с 08:00
HOUR_END   = 22  # до 22:00 (включительно, последний слот начинается в 22:xx не отправляется)

RSS_FEEDS_RU = [
    # Русскоязычные — перевод не нужен
    "https://feeds.bbci.co.uk/russian/business/rss.xml",  # BBC Бизнес
    "https://rss.dw.com/rdf/rss-ru-wirtschaft",            # DW Экономика
    "https://ru.euronews.com/rss?level=theme&name=business", # Euronews Бизнес
    "https://www.pravda.com.ua/rss/economics/",             # УП Экономика
]

RSS_FEEDS_EN = [
    # Английские — будут переводиться автоматически
    "https://www.forbes.com/business/feed/",               # Forbes
    "https://feeds.bloomberg.com/technology/news.rss",     # Bloomberg
    "https://inc.com/rss",                                  # Inc. Magazine
    "https://hbr.org/feed",                                 # Harvard Business Review
    "https://techcrunch.com/feed/",                         # TechCrunch
]

SENT_FILE    = "sent_ids.json"
PENDING_FILE = "pending.json"
# ─────────────────────────────────────────────────────────────────────────────


# ─── Хранилище ───────────────────────────────────────────────────────────────
def load_json(path: str, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_sent() -> set:
    return set(load_json(SENT_FILE, []))


def load_pending() -> dict:
    return load_json(PENDING_FILE, {})


def save_pending(pending: dict):
    save_json(PENDING_FILE, pending)
# ─────────────────────────────────────────────────────────────────────────────


def extract_image(entry) -> str | None:
    """Ищет URL картинки в RSS-записи по нескольким стандартным местам."""

    # 1. media:content или media:thumbnail
    for media in entry.get("media_content", []):
        url = media.get("url", "")
        if url and re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", url, re.I):
            return url

    for thumb in entry.get("media_thumbnail", []):
        url = thumb.get("url", "")
        if url:
            return url

    # 2. enclosure (podcasts / изображения)
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/"):
            return enc.get("href") or enc.get("url")

    # 3. <img> в тексте summary или content
    for field in ("summary", "content"):
        text = ""
        val = entry.get(field)
        if isinstance(val, list):
            text = " ".join(v.get("value", "") for v in val)
        elif isinstance(val, str):
            text = val
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text, re.I)
        if match:
            url = match.group(1)
            if url.startswith("http"):
                return url

    return None


def translate_to_ru(text: str) -> str:
    """Переводит текст на русский если он не на русском."""
    try:
        if not text:
            return text
        translated = GoogleTranslator(source="auto", target="ru").translate(text[:4500])
        return translated or text
    except Exception as e:
        logger.warning(f"Ошибка перевода: {e}")
        return text


def format_message(entry, translate: bool = False) -> str:
    title   = entry.get("title", "Без заголовка").strip()
    link    = entry.get("link", "")
    summary = entry.get("summary", "")

    summary = re.sub(r"<[^>]+>", "", summary).strip()

    if translate:
        title   = translate_to_ru(title)
        summary = translate_to_ru(summary[:500])

    if summary:
        summary = summary[:300].strip()
        if len(summary) >= 300:
            summary += "…"
        return f"📰 <b>{title}</b>\n\n{summary}\n\n<a href='{link}'>Читать далее →</a>"
    return f"📰 <b>{title}</b>\n\n<a href='{link}'>Читать далее →</a>"


async def send_for_moderation(bot: Bot, entry_id: str, text: str, image_url: str | None, pending: dict):
    """Отправляет пост администратору на проверку."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve:{entry_id}"),
            InlineKeyboardButton("❌ Отклонить",    callback_data=f"reject:{entry_id}"),
        ]
    ])

    header = "🔍 <b>Новый пост на модерации:</b>\n\n"

    if image_url:
        try:
            msg = await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=image_url,
                caption=header + text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception:
            # Если картинка не загрузилась — отправляем без неё
            logger.warning(f"Не удалось загрузить фото {image_url}, отправляю текст.")
            image_url = None
            msg = await bot.send_message(
                chat_id=ADMIN_ID,
                text=header + text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    else:
        msg = await bot.send_message(
            chat_id=ADMIN_ID,
            text=header + text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )

    pending[entry_id] = {
        "text":       text,
        "image_url":  image_url,
        "message_id": msg.message_id,
        "created_at": datetime.now().isoformat(),
    }
    save_pending(pending)
    logger.info(f"Отправлено на модерацию {'🖼 с фото' if image_url else '📝 без фото'}: {text[:60]}…")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие кнопок ✅ / ❌."""
    query = update.callback_query
    await query.answer()

    action, entry_id = query.data.split(":", 1)
    pending  = load_pending()
    sent_ids = load_sent()

    if entry_id not in pending:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⚠️ Этот пост уже был обработан.")
        return

    item = pending.pop(entry_id)
    save_pending(pending)

    if action == "approve":
        try:
            image_url = item.get("image_url")
            if image_url:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=image_url,
                    caption=item["text"],
                    parse_mode=ParseMode.HTML,
                )
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=item["text"],
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
            sent_ids.add(entry_id)
            save_json(SENT_FILE, list(sent_ids))
            await query.edit_message_text(
                "✅ <b>Опубликовано!</b>\n\n" + item["text"],
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
            logger.info(f"Опубликовано: {entry_id[:60]}")

            # Запускаем генерацию видео для TikTok/Instagram
            if VIDEO_ENABLED:
                # Извлекаем заголовок и summary из текста поста
                lines = item["text"].replace("<b>", "").replace("</b>", "").split("\n")
                title   = lines[0].replace("📰 ", "").strip() if lines else "Новость"
                summary = lines[2].strip() if len(lines) > 2 else ""
                asyncio.create_task(
                    send_video_to_admin(
                        context.bot, title, summary, item.get("image_url")
                    )
                )
        except Exception as e:
            logger.error(f"Ошибка публикации: {e}")
            await query.message.reply_text(f"❌ Ошибка при публикации: {e}")

    elif action == "reject":
        sent_ids.add(entry_id)  # помечаем, чтобы не появилось снова
        save_json(SENT_FILE, list(sent_ids))
        await query.edit_message_text(
            "🗑 <b>Отклонено.</b>\n\n" + item["text"],
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )
        logger.info(f"Отклонено: {entry_id[:60]}")


def now_kyiv() -> datetime:
    """Текущее время по Киеву."""
    return datetime.now(KYIV_TZ)


def is_working_hour() -> bool:
    """Проверяет, входит ли текущий час в рабочий диапазон по Киеву."""
    return HOUR_START <= now_kyiv().hour < HOUR_END


def seconds_until_work_start() -> float:
    """Секунды до начала рабочего времени (8:00 по Киеву)."""
    now = now_kyiv()
    today_start = now.replace(hour=HOUR_START, minute=0, second=0, microsecond=0)
    if now >= today_start:
        # Уже прошло сегодня — ждём завтра
        today_start += timedelta(days=1)
    return (today_start - now).total_seconds()


def get_random_slots_for_hour(n: int = 2) -> list:
    """
    Генерирует N случайных моментов внутри текущего часа по Киеву.
    Час делится на N равных окон, в каждом выбирается случайная секунда.
    """
    now = now_kyiv()
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    window_minutes = 60 // n
    slots = []

    for i in range(n):
        window_start = hour_start + timedelta(minutes=i * window_minutes)
        window_end   = hour_start + timedelta(minutes=(i + 1) * window_minutes - 1, seconds=59)
        rand_second  = random.randint(0, int((window_end - window_start).total_seconds()))
        slot = window_start + timedelta(seconds=rand_second)
        if slot <= now:
            slot += timedelta(hours=1)
        slots.append(slot)

    return sorted(slots)


async def fetch_new_entries(sent_ids: set, pending: dict) -> list:
    already_queued = set(pending.keys())
    new_entries = []

    for feed_url in RSS_FEEDS_RU:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                eid = entry.get("id") or entry.get("link")
                if eid and eid not in sent_ids and eid not in already_queued:
                    new_entries.append((eid, entry, False))  # False = не переводить
        except Exception as e:
            logger.error(f"Ошибка чтения {feed_url}: {e}")

    for feed_url in RSS_FEEDS_EN:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                eid = entry.get("id") or entry.get("link")
                if eid and eid not in sent_ids and eid not in already_queued:
                    new_entries.append((eid, entry, True))  # True = переводить
        except Exception as e:
            logger.error(f"Ошибка чтения {feed_url}: {e}")

    def parse_date(item):
        ep = item[1].get("published_parsed")
        return datetime(*ep[:6]) if ep else datetime.min

    new_entries.sort(key=parse_date)
    return new_entries

async def rss_poller(app: Application):
    """
    Умный планировщик с учётом рабочих часов по Киеву (08:00–22:00).
    В каждом рабочем часе отправляет POSTS_PER_HOUR постов в случайное время.
    Вне рабочих часов — спит до 08:00.
    """
    bot = app.bot
    logger.info(
        f"Планировщик запущен: {POSTS_PER_HOUR} поста(ов)/час "
        f"с {HOUR_START}:00 до {HOUR_END}:00 по Киеву."
    )

    while True:
        # Если сейчас не рабочее время — ждём начала следующего рабочего дня
        if not is_working_hour():
            wait = seconds_until_work_start()
            wake_at = (now_kyiv() + timedelta(seconds=wait)).strftime("%d.%m %H:%M")
            logger.info(f"⏸ Нерабочее время. Сплю до {wake_at} (Киев).")
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=f"😴 Бот уходит спать. Следующие посты — завтра в {HOUR_START}:00 по Киеву.",
            ) if now_kyiv().hour == HOUR_END else None
            await asyncio.sleep(wait)
            continue

        # Планируем слоты для текущего рабочего часа
        slots = get_random_slots_for_hour(POSTS_PER_HOUR)
        slot_strs = [s.strftime("%H:%M:%S") for s in slots]
        logger.info(f"🕐 Слоты по Киеву: {', '.join(slot_strs)}")

        for slot in slots:
            wait = (slot - now_kyiv()).total_seconds()
            if wait > 0:
                logger.info(
                    f"Следующий пост в {slot.strftime('%H:%M:%S')} Киев "
                    f"(через {int(wait//60)}м {int(wait%60)}с)"
                )
                await asyncio.sleep(wait)

            # Проверяем ещё раз — вдруг пока спали вышли за 22:00
            if not is_working_hour():
                logger.info("Рабочее время закончилось, пропускаю слот.")
                break

            sent_ids = load_sent()
            pending  = load_pending()
            entries  = await fetch_new_entries(sent_ids, pending)

            if not entries:
                logger.info("Нет новых новостей для отправки.")
                continue

            eid, entry, need_translate = entries[0]
text      = format_message(entry, translate=need_translate)
image_url = extract_image(entry)

            try:
                await send_for_moderation(bot, eid, text, image_url, pending)
                logger.info(f"✅ Пост отправлен в {now_kyiv().strftime('%H:%M:%S')} (Киев)")
            except Exception as e:
                logger.error(f"Ошибка при отправке на модерацию: {e}")

        # Ждём начала следующего часа
        now = now_kyiv()
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        wait_to_next = (next_hour - now_kyiv()).total_seconds() + 2
        logger.info(f"Цикл часа завершён. Следующий через {int(wait_to_next//60)} мин.")
        await asyncio.sleep(wait_to_next)


async def post_init(app: Application):
    asyncio.create_task(rss_poller(app))


def main():
    if ADMIN_ID == 0:
        raise ValueError("Укажите ADMIN_ID — ваш числовой Telegram ID")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info(f"Бот запущен. Канал: {CHANNEL_ID} | Администратор: {ADMIN_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
