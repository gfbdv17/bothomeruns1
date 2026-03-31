import os
import json
import logging
import asyncio
import re
from datetime import datetime, timezone, date
from pathlib import Path

import feedparser
import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TWITTER_USERNAME    = os.environ.get("TWITTER_USERNAME", "MLBHRvideos")
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
CHANNEL_USERNAME    = os.environ.get("CHANNEL_USERNAME", "HOMERUNSMLB")
CHANNEL_LINK        = os.environ.get("CHANNEL_LINK", "https://t.me/HOMERUNSMLB")
CHECK_INTERVAL_MIN  = int(os.environ.get("CHECK_INTERVAL_MIN", "5"))
SEEN_FILE           = Path(os.environ.get("SEEN_FILE", "seen_tweets.json"))

NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.net",
]

MLB_API = "https://statsapi.mlb.com/api/v1"

# ── Estado persistente ────────────────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ── RSS ───────────────────────────────────────────────────────────────────────
async def fetch_rss(username: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for instance in NITTER_INSTANCES:
            url = f"{instance}/{username}/rss"
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    feed = feedparser.parse(resp.text)
                    if feed.entries:
                        log.info(f"RSS ok desde {instance}")
                        return parse_entries(feed.entries)
            except Exception as e:
                log.warning(f"{instance} falló: {e}")
    return []

def parse_entries(entries) -> list[dict]:
    tweets = []
    for entry in entries:
        media_url = ""
        content = entry.get("summary", "")
        img_match = re.search(r'<img[^>]+src="([^"]+)"', content)
        if img_match:
            media_url = img_match.group(1)
            media_url = re.sub(r"https?://[^/]+/pic/", "https://pbs.twimg.com/", media_url)
            media_url = media_url.replace("%2F", "/")
        text = re.sub(r"<[^>]+>", "", content).strip()
        text = re.sub(r"\s+", " ", text).strip()
        tweets.append({
            "id": entry.get("id", entry.get("link", "")),
            "text": text,
            "link": entry.get("link", ""),
            "media_url": media_url,
        })
    return tweets

# ── Extraer nombre del jugador del tweet ──────────────────────────────────────
def extract_player_name(text: str) -> str | None:
    """
    El formato de @MLBHRvideos es:
    'FirstName LastName Nth Home Run of...'
    Tomamos las primeras 2 o 3 palabras antes de 'Home Run'
    """
    match = re.match(r"^(.+?)\s+\d{1,3}(?:st|nd|rd|th)\s+Home Run", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # fallback: primeras 2 palabras
    words = text.split()
    if len(words) >= 2:
        return f"{words[0]} {words[1]}"
    return None

# ── MLB Stats API ─────────────────────────────────────────────────────────────
async def search_player_id(client: httpx.AsyncClient, name: str) -> int | None:
    try:
        resp = await client.get(
            f"{MLB_API}/people/search",
            params={"names": name, "sportId": 1},
            timeout=10,
        )
        data = resp.json()
        people = data.get("people", [])
        if people:
            return people[0]["id"]
    except Exception as e:
        log.warning(f"Búsqueda de jugador '{name}' falló: {e}")
    return None

async def get_todays_homeruns(client: httpx.AsyncClient) -> list[dict]:
    """Obtiene todos los home runs del día de hoy desde MLB Stats API."""
    today = date.today().strftime("%Y-%m-%d")
    try:
        resp = await client.get(
            f"{MLB_API}/schedule",
            params={
                "sportId": 1,
                "date": today,
                "hydrate": "plays(result,about,matchup,pitchIndex,playEvents(details,count,pitchData,hitData))",
            },
            timeout=15,
        )
        data = resp.json()
        homeruns = []
        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                game_pk = game.get("gamePk")
                plays = game.get("plays", {}).get("allPlays", [])
                for play in plays:
                    result = play.get("result", {})
                    if result.get("eventType") == "home_run":
                        matchup = play.get("matchup", {})
                        batter = matchup.get("batter", {})
                        pitcher = matchup.get("pitcher", {})
                        # Buscar hitData en los eventos del play
                        hit_data = {}
                        for event in play.get("playEvents", []):
                            hd = event.get("hitData", {})
                            if hd:
                                hit_data = hd
                        homeruns.append({
                            "batter_name": batter.get("fullName", ""),
                            "batter_id": batter.get("id"),
                            "pitcher_name": pitcher.get("fullName", ""),
                            "description": result.get("description", ""),
                            "distance": hit_data.get("totalDistance", 0),
                            "exit_velocity": hit_data.get("launchSpeed", 0),
                            "launch_angle": hit_data.get("launchAngle", 0),
                            "game_pk": game_pk,
                        })
        return homeruns
    except Exception as e:
        log.warning(f"Error obteniendo HRs del día: {e}")
        return []

async def find_hr_data(player_name: str) -> dict | None:
    """Busca los datos del home run del jugador en los juegos de hoy."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        homeruns = await get_todays_homeruns(client)
        if not homeruns:
            log.info("No se encontraron HRs hoy en MLB API.")
            return None

        # Buscar por nombre (fuzzy: comparar palabras)
        name_lower = player_name.lower()
        for hr in homeruns:
            batter = hr["batter_name"].lower()
            # Coincidencia si el apellido o nombre completo coincide
            if name_lower in batter or batter in name_lower:
                log.info(f"HR encontrado para {player_name}: {hr}")
                return hr

        log.info(f"No se encontró HR para '{player_name}' en MLB API hoy.")
        return None

# ── Formatear mensaje ─────────────────────────────────────────────────────────
def escape_md(text: str) -> str:
    chars = r"\_*[]()~`>#+-=|{}.!"
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text

def format_message(tweet: dict, hr_data: dict | None) -> str:
    raw = tweet["text"]

    # Título: primera línea del tweet limpia
    title_line = raw.split("\n")[0] if "\n" in raw else raw[:120]
    title = escape_md(title_line)

    if hr_data and hr_data.get("distance"):
        distance    = hr_data.get("distance", "N/A")
        exit_vel    = hr_data.get("exit_velocity", "N/A")
        launch      = hr_data.get("launch_angle", "N/A")
        pitcher     = escape_md(hr_data.get("pitcher_name", "N/A"))
        description = escape_md(hr_data.get("description", ""))

        msg = (
            f"⚾ *{title}*\n\n"
            f"📏 Distance: {distance}ft\n"
            f"💥 Exit Velocity: {exit_vel} MPH\n"
            f"📐 Launch Angle: {launch}°\n"
            f"🎯 Pitcher: {pitcher}\n"
        )
        if description:
            msg += f"\n_{description}_"
    else:
        # Sin datos de MLB API, usar texto del tweet tal cual
        lines = [escape_md(l) for l in raw.split("\n") if l.strip()]
        msg = f"⚾ *{lines[0]}*\n\n" + "\n".join(lines[1:]) if len(lines) > 1 else f"⚾ *{escape_md(raw[:400])}*"

    return msg

# ── Botón inline ──────────────────────────────────────────────────────────────
def get_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(text=f"📢 {CHANNEL_USERNAME}", url=CHANNEL_LINK)
    ]])

# ── Enviar a Telegram ─────────────────────────────────────────────────────────
async def send_to_telegram(bot: Bot, tweet: dict, hr_data: dict | None):
    caption = format_message(tweet, hr_data)
    keyboard = get_keyboard()

    try:
        if tweet["media_url"]:
            try:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=tweet["media_url"],
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard,
                )
                return
            except Exception as e:
                log.warning(f"Foto falló, enviando texto: {e}")

        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )
    except Exception as e:
        log.error(f"Error Telegram: {e}")

# ── Job principal ─────────────────────────────────────────────────────────────
async def check_and_post(bot: Bot):
    seen = load_seen()
    tweets = await fetch_rss(TWITTER_USERNAME)

    if not tweets:
        log.info("Sin tweets o RSS no disponible.")
        return

    nuevos = 0
    for tweet in reversed(tweets):
        if tweet["id"] in seen:
            continue

        # Buscar datos del HR en MLB API
        player_name = extract_player_name(tweet["text"])
        hr_data = None
        if player_name:
            log.info(f"Buscando HR de '{player_name}' en MLB API...")
            hr_data = await find_hr_data(player_name)

        await send_to_telegram(bot, tweet, hr_data)
        seen.add(tweet["id"])
        nuevos += 1
        await asyncio.sleep(2)

    if nuevos:
        save_seen(seen)
        log.info(f"{nuevos} tweet(s) enviados.")
    else:
        log.info("Sin tweets nuevos.")

# ── Health-check ──────────────────────────────────────────────────────────────
async def health_server():
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
    log.info(f"Health-check en :{port}")

# ── Entrypoint ────────────────────────────────────────────────────────────────
async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    me = await bot.get_me()
    log.info(f"Bot conectado: @{me.username}")

    await health_server()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_and_post,
        "interval",
        minutes=CHECK_INTERVAL_MIN,
        args=[bot],
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    log.info(f"Scheduler iniciado. Revisando cada {CHECK_INTERVAL_MIN} min.")

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
