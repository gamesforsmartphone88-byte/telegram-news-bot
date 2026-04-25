import asyncio
import logging
import json
import os
import textwrap
import requests
import numpy as np
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from gtts import gTTS
from moviepy.editor import (
    ImageClip, AudioFileClip, CompositeVideoClip,
    concatenate_videoclips, ColorClip
)
from moviepy.video.fx.fadein import fadein
from moviepy.video.fx.fadeout import fadeout
from telegram import Bot
from telegram.ext import Application

# YouTube API
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")   # тот же бот
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0"))               # ваш Telegram ID
CHANNEL_TAG = os.getenv("CHANNEL_TAG", "@ваш_канал")        # подпись в видео

# YouTube
YOUTUBE_ENABLED      = os.getenv("YOUTUBE_ENABLED", "true").lower() == "true"
YOUTUBE_CREDENTIALS  = "youtube_token.json"    # сохранённый OAuth токен
YOUTUBE_CLIENT_FILE  = "client_secrets.json"   # скачать из Google Cloud Console
YOUTUBE_CATEGORY_ID  = "25"                    # 25 = News & Politics
YOUTUBE_PRIVACY      = "public"                # public / unlisted / private

# Размер видео — вертикальный формат TikTok/Reels/Shorts
VIDEO_W, VIDEO_H = 1080, 1920
FONT_DIR = Path("fonts")   # папка со шрифтами (см. README)
TMP_DIR  = Path("tmp")
TMP_DIR.mkdir(exist_ok=True)
# ─────────────────────────────────────────────────────────────────────────────


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Загружает шрифт из папки fonts/ или системный запасной."""
    names = ["Roboto-Bold.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"] if bold \
       else ["Roboto-Regular.ttf", "Arial.ttf", "DejaVuSans.ttf"]
    for name in names:
        path = FONT_DIR / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def fetch_image(url: str | None) -> Image.Image | None:
    """Скачивает картинку по URL."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e:
        logger.warning(f"Не удалось загрузить картинку: {e}")
        return None


def make_background(img: Image.Image | None) -> Image.Image:
    """
    Создаёт фон 1080×1920:
    - Если есть картинка — растягивает с blur + тёмный оверлей
    - Если нет — градиентный фон
    """
    canvas = Image.new("RGB", (VIDEO_W, VIDEO_H))

    if img:
        # Заполняем весь холст картинкой (crop по центру)
        ratio = max(VIDEO_W / img.width, VIDEO_H / img.height)
        new_w = int(img.width * ratio)
        new_h = int(img.height * ratio)
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)
        x = (new_w - VIDEO_W) // 2
        y = (new_h - VIDEO_H) // 2
        img_cropped = img_resized.crop((x, y, x + VIDEO_W, y + VIDEO_H))

        # Сильный blur + затемнение
        blurred = img_cropped.filter(ImageFilter.GaussianBlur(radius=25))
        darkened = ImageEnhance.Brightness(blurred).enhance(0.35)
        canvas.paste(darkened)

        # Оригинал картинки по центру верхней половины (чёткий)
        thumb_h = int(VIDEO_H * 0.45)
        thumb_w = int(thumb_h * img.width / img.height)
        if thumb_w > VIDEO_W - 80:
            thumb_w = VIDEO_W - 80
            thumb_h = int(thumb_w * img.height / img.width)
        thumb = img.resize((thumb_w, thumb_h), Image.LANCZOS)
        tx = (VIDEO_W - thumb_w) // 2
        ty = 140
        canvas.paste(thumb, (tx, ty))

        # Скруглённая рамка вокруг картинки (имитация через overlay)
        overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
        draw_ov = ImageDraw.Draw(overlay)
        draw_ov.rounded_rectangle(
            [tx - 6, ty - 6, tx + thumb_w + 6, ty + thumb_h + 6],
            radius=18, outline=(255, 255, 255, 180), width=3
        )
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    else:
        # Градиент от тёмно-синего к чёрному
        arr = np.zeros((VIDEO_H, VIDEO_W, 3), dtype=np.uint8)
        for y in range(VIDEO_H):
            t = y / VIDEO_H
            r = int(10 + t * 5)
            g = int(10 + t * 15)
            b = int(40 + t * 20)
            arr[y, :] = [r, g, b]
        canvas = Image.fromarray(arr)

    return canvas


def draw_text_block(
    canvas: Image.Image,
    text: str,
    y_start: int,
    font_size: int = 52,
    bold: bool = False,
    color: tuple = (255, 255, 255),
    max_width: int = VIDEO_W - 100,
    line_spacing: int = 14,
    shadow: bool = True,
    highlight_first_line: bool = False,
) -> int:
    """Рисует текстовый блок с переносами и тенью. Возвращает Y после блока."""
    draw = ImageDraw.Draw(canvas)
    font = load_font(font_size, bold=bold)
    highlight_font = load_font(font_size + 4, bold=True) if highlight_first_line else font

    # Разбиваем на строки
    words = text.split()
    lines = []
    current = []
    for word in words:
        test = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))

    y = y_start
    for i, line in enumerate(lines):
        f = highlight_font if (highlight_first_line and i == 0) else font
        bbox = draw.textbbox((0, 0), line, font=f)
        line_w = bbox[2] - bbox[0]
        x = (VIDEO_W - line_w) // 2

        if shadow:
            draw.text((x + 3, y + 3), line, font=f, fill=(0, 0, 0, 160))
        draw.text((x, y), line, font=f, fill=color)

        line_h = bbox[3] - bbox[1]
        y += line_h + line_spacing

    return y


def make_frame(
    title: str,
    summary: str,
    channel_tag: str,
    img: Image.Image | None,
    progress: float = 1.0,   # 0.0 → 1.0, для анимации
) -> np.ndarray:
    """Рисует один кадр видео."""
    canvas = make_background(img)
    draw = ImageDraw.Draw(canvas)

    # Верхняя плашка — Breaking News
    bar_h = 72
    bar_y = 60 if img is None else int(VIDEO_H * 0.52)
    draw.rounded_rectangle([50, bar_y, VIDEO_W - 50, bar_y + bar_h],
                            radius=12, fill=(220, 40, 40))
    label_font = load_font(30, bold=True)
    draw.text((VIDEO_W // 2, bar_y + bar_h // 2), "🔴 НОВОСТИ",
              font=label_font, fill=(255, 255, 255), anchor="mm")

    # Заголовок
    title_y = bar_y + bar_h + 30
    title_y = draw_text_block(
        canvas, title, title_y,
        font_size=58, bold=True,
        color=(255, 255, 255),
        highlight_first_line=False,
    )

    # Разделитель
    title_y += 20
    draw.line([(80, title_y), (VIDEO_W - 80, title_y)],
              fill=(255, 255, 255, 120), width=2)
    title_y += 30

    # Краткий текст
    short = summary[:200] + ("…" if len(summary) > 200 else "")
    title_y = draw_text_block(
        canvas, short, title_y,
        font_size=42, bold=False,
        color=(220, 220, 220),
    )

    # Нижняя плашка с тегом канала
    bottom_y = VIDEO_H - 130
    draw.rounded_rectangle([50, bottom_y, VIDEO_W - 50, bottom_y + 80],
                            radius=12, fill=(0, 0, 0, 180))
    tag_font = load_font(38, bold=True)
    draw.text((VIDEO_W // 2, bottom_y + 40),
              f"Подписывайся → {channel_tag}",
              font=tag_font, fill=(100, 200, 255), anchor="mm")

    # Анимация: fade-in через прозрачность
    if progress < 1.0:
        alpha = int(255 * progress)
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 255 - alpha))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    return np.array(canvas)


def generate_tts(text: str, path: str, lang: str = "ru"):
    """Генерирует озвучку через gTTS."""
    tts = gTTS(text=text, lang=lang, slow=False)
    tts.save(path)
    logger.info(f"TTS сохранён: {path}")


# ─── YouTube ──────────────────────────────────────────────────────────────────
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_youtube_client():
    """
    Возвращает авторизованный YouTube API клиент.
    При первом запуске открывает браузер для OAuth.
    Последующие запуски используют сохранённый токен.
    """
    creds = None

    if os.path.exists(YOUTUBE_CREDENTIALS):
        creds = Credentials.from_authorized_user_file(YOUTUBE_CREDENTIALS, YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(YOUTUBE_CLIENT_FILE):
                raise FileNotFoundError(
                    f"Файл {YOUTUBE_CLIENT_FILE} не найден!\n"
                    "Скачайте его из Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                YOUTUBE_CLIENT_FILE, YOUTUBE_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(YOUTUBE_CREDENTIALS, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(video_path: str, title: str, description: str) -> str | None:
    """
    Загружает видео на YouTube как Shorts.
    Возвращает URL видео или None при ошибке.
    """
    try:
        youtube = get_youtube_client()

        # Хэштег #Shorts обязателен для попадания в YouTube Shorts
        shorts_title    = title[:90]   # лимит YouTube — 100 символов
        shorts_desc     = (
            f"{description}\n\n"
            f"Подписывайся на наш Telegram: {CHANNEL_TAG}\n\n"
            "#Shorts #Новости #News"
        )

        body = {
            "snippet": {
                "title":       shorts_title,
                "description": shorts_desc,
                "tags":        ["новости", "shorts", "news", CHANNEL_TAG],
                "categoryId":  YOUTUBE_CATEGORY_ID,
            },
            "status": {
                "privacyStatus":           YOUTUBE_PRIVACY,
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            video_path,
            mimetype="video/mp4",
            resumable=True,
            chunksize=5 * 1024 * 1024,  # 5 МБ чанки
        )

        request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(f"YouTube upload: {int(status.progress() * 100)}%")

        video_id  = response["id"]
        video_url = f"https://youtube.com/shorts/{video_id}"
        logger.info(f"Загружено на YouTube: {video_url}")
        return video_url

    except Exception as e:
        logger.error(f"Ошибка загрузки на YouTube: {e}")
        return None
# ─────────────────────────────────────────────────────────────────────────────


def build_video(title: str, summary: str, image_url: str | None, output_path: str):
    """Собирает финальное видео."""
    logger.info("Генерирую видео…")
    img = fetch_image(image_url)

    # --- TTS ---
    tts_text = f"{title}. {summary[:300]}"
    tts_path = str(TMP_DIR / "speech.mp3")
    generate_tts(tts_text, tts_path)
    audio = AudioFileClip(tts_path)
    duration = audio.duration + 1.5  # небольшой хвост после озвучки

    FPS = 24

    # --- Кадры с анимацией появления (первые 0.5 сек) ---
    fade_frames = int(FPS * 0.5)
    frames = []

    for i in range(fade_frames):
        progress = i / fade_frames
        frames.append(make_frame(title, summary, CHANNEL_TAG, img, progress))

    # Основной статичный кадр
    main_frame = make_frame(title, summary, CHANNEL_TAG, img, 1.0)
    static_duration = duration - 0.5
    static_clip = ImageClip(main_frame, duration=static_duration)

    # Fade-in клип
    fade_clip = concatenate_videoclips([
        ImageClip(f, duration=1 / FPS) for f in frames
    ])

    # Финальный клип
    video = concatenate_videoclips([fade_clip, static_clip])
    video = video.set_audio(audio)
    video = fadeout(video, 0.5)

    video.write_videofile(
        output_path,
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        temp_audiofile=str(TMP_DIR / "temp_audio.m4a"),
        remove_temp=True,
        logger=None,
    )
    logger.info(f"Видео готово: {output_path}")


async def send_video_to_admin(bot: Bot, title: str, summary: str, image_url: str | None):
    """Генерирует видео, отправляет в личку и загружает на YouTube."""
    output = str(TMP_DIR / "short.mp4")

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🎬 Генерирую видео для:\n<b>{title}</b>",
        parse_mode="HTML",
    )

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, build_video, title, summary, image_url, output
        )

        # 1. Отправляем в Telegram личку
        with open(output, "rb") as f:
            await bot.send_video(
                chat_id=ADMIN_ID,
                video=f,
                caption=f"📱 <b>{title}</b>\n\nГотово к публикации в TikTok / Instagram!",
                parse_mode="HTML",
                width=VIDEO_W,
                height=VIDEO_H,
                supports_streaming=True,
            )
        logger.info("Видео отправлено администратору.")

        # 2. Загружаем на YouTube Shorts
        if YOUTUBE_ENABLED:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text="⏳ Загружаю на YouTube Shorts…",
            )
            yt_url = await loop.run_in_executor(
                None, upload_to_youtube, output, title, summary
            )
            if yt_url:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"✅ <b>Опубликовано на YouTube!</b>\n{yt_url}",
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text="⚠️ Не удалось загрузить на YouTube. Проверьте логи.",
                )

    except Exception as e:
        logger.error(f"Ошибка генерации видео: {e}")
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка генерации видео: {e}")
    finally:
        if os.path.exists(output):
            os.remove(output)


# ─── Точка входа для запуска напрямую ────────────────────────────────────────
# Этот файл можно запустить отдельно для теста:
# python video_bot.py
if __name__ == "__main__":
    import sys

    async def test():
        bot = Bot(token=BOT_TOKEN)
        title   = sys.argv[1] if len(sys.argv) > 1 else "Тестовый заголовок новости"
        summary = sys.argv[2] if len(sys.argv) > 2 else \
            "Это тестовый текст для проверки генерации видео. Бот создаёт короткое вертикальное видео с анимацией и озвучкой."
        image_url = sys.argv[3] if len(sys.argv) > 3 else None
        await send_video_to_admin(bot, title, summary, image_url)

    asyncio.run(test())
