import os
import json
import logging
import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

import tweepy
import httpx
from telegram import Bot
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config from env ───────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ["TWITTER_BEARER_TOKEN"]
TWITTER_USERNAME     = os.environ.get("TWITTER_USERNAME", "MLBHomeRuns")   # cuenta fuente
TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID  = os.environ["TELEGRAM_CHANNEL_ID"]   # ej: @micanal o -100xxxxxxxx
CHECK_INTERVAL_MIN   = int(os.environ.get("CHECK_INTERVAL_MIN", "5"))
SEEN_FILE            = Path(os.environ.get("SEEN_FILE", "seen_tweets.json"))

# ── Estado persistente (IDs ya enviados) ─────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ── Twitter client ────────────────────────────────────────────────────────────
twitter = tweepy.Client(bearer_token=TWITTER_BEARER_TOKEN, wait_on_rate_limit=True)

def get_user_id(username: str) -> str:
    resp = twitter.get_user(username=username)
    if resp.data is None:
        raise ValueError(f"Usuario de Twitter '{username}' no encontrado.")
    return resp.data.id

def fetch_latest_tweets(user_id: str, since_id: str | None) -> list[dict]:
    """Devuelve tweets nuevos del usuario, con expansiones de media."""
    kwargs = dict(
        id=user_id,
        max_results=10,
        tweet_fields=["created_at", "text", "attachments"],
        expansions=["attachments.media_keys"],
        media_fields=["url", "preview_image_url", "type"],
    )
    if since_id:
        kwargs["since_id"] = since_id

    resp = twitter.get_users_tweets(**kwargs)
    if not resp.data:
        return []

    # Indexar media
    media_map: dict[str, str] = {}
    if resp.includes and "media" in resp.includes:
        for m in resp.includes["media"]:
            url = m.url or m.preview_image_url or ""
            if url:
                media_map[m.media_key] = url

    tweets = []
    for t in resp.data:
        media_urls = []
        if t.attachments and "media_keys" in t.attachments:
            for mk in t.attachments["media_keys"]:
                if mk in media_map:
                    media_urls.append(media_map[mk])
        tweets.append({
            "id": str(t.id),
            "text": t.text,
            "created_at": t.created_at,
            "media_urls": media_urls,
        })

    return tweets  # más reciente primero; revertimos para enviar en orden

# ── Formatear mensaje Telegram ────────────────────────────────────────────────
HOMERUN_KEYWORDS = re.compile(
    r"\b(home\s?run|homer|HR|jonrón|cuadrangular|grand\s?slam)\b",
    re.IGNORECASE,
)

def is_homerun_tweet(text: str) -> bool:
    return bool(HOMERUN_KEYWORDS.search(text))

def format_message(tweet: dict) -> str:
    text = tweet["text"]
    # Limpiar URLs de Twitter del texto
    text = re.sub(r"https://t\.co/\S+", "", text).strip()
    ts = ""
    if tweet["created_at"]:
        ts = tweet["created_at"].strftime("🕐 %d/%m/%Y %H:%M UTC")
    return f"⚾ *HOME RUN*\n\n{text}\n\n{ts}"

# ── Enviar a Telegram ─────────────────────────────────────────────────────────
async def send_tweet_to_telegram(bot: Bot, tweet: dict):
    caption = format_message(tweet)
    photos = [u for u in tweet["media_urls"] if u]

    try:
        if photos:
            await bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=photos[0],
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        log.info(f"Enviado tweet {tweet['id']} a Telegram.")
    except Exception as e:
        log.error(f"Error enviando tweet {tweet['id']}: {e}")

# ── Job principal ─────────────────────────────────────────────────────────────
_user_id: str | None = None
_last_seen_id: str | None = None

async def check_and_post(bot: Bot):
    global _user_id, _last_seen_id
    seen = load_seen()

    try:
        if _user_id is None:
            _user_id = get_user_id(TWITTER_USERNAME)
            log.info(f"User ID de @{TWITTER_USERNAME}: {_user_id}")

        tweets = fetch_latest_tweets(_user_id, _last_seen_id)
        if not tweets:
            log.info("Sin tweets nuevos.")
            return

        # Procesar en orden cronológico (más antiguo primero)
        for tweet in reversed(tweets):
            if tweet["id"] in seen:
                continue
            # @MLBHRvideos es cuenta dedicada a home runs → filtro desactivado.
            # Para reactivarlo, descomenta las 4 líneas siguientes:
            # if not is_homerun_tweet(tweet["text"]):
            #     log.info(f"Tweet {tweet['id']} ignorado (no es home run).")
            #     seen.add(tweet["id"])
            #     continue

            await send_tweet_to_telegram(bot, tweet)
            seen.add(tweet["id"])
            _last_seen_id = tweet["id"]

        save_seen(seen)

    except tweepy.TweepyException as e:
        log.error(f"Error de Twitter API: {e}")
    except Exception as e:
        log.exception(f"Error inesperado: {e}")

# ── Health-check HTTP (para UptimeRobot) ─────────────────────────────────────
async def health_server():
    """Servidor HTTP mínimo en el puerto $PORT para que Render no mate el proceso."""
    from aiohttp import web

    async def handle(_req):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)

    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health-check server escuchando en :{port}")

# ── Entrypoint ────────────────────────────────────────────────────────────────
async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    me = await bot.get_me()
    log.info(f"Bot conectado: @{me.username}")

    # Servidor de salud (requerido por Render + UptimeRobot)
    await health_server()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_and_post,
        "interval",
        minutes=CHECK_INTERVAL_MIN,
        args=[bot],
        next_run_time=datetime.now(timezone.utc),  # ejecutar inmediatamente al arrancar
    )
    scheduler.start()
    log.info(f"Scheduler iniciado. Revisando cada {CHECK_INTERVAL_MIN} min.")

    # Mantener vivo
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
