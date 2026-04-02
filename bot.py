import os
import json
import logging
import asyncio
import re
from datetime import datetime, timezone, date
from pathlib import Path

import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TWITTER_USERNAME    = os.environ.get("TWITTER_USERNAME", "MLBHRVideos")
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
CHANNEL_USERNAME    = os.environ.get("CHANNEL_USERNAME", "HOMERUNSMLB")
CHANNEL_LINK        = os.environ.get("CHANNEL_LINK", "https://t.me/HOMERUNSMLB")
CHECK_INTERVAL_MIN  = int(os.environ.get("CHECK_INTERVAL_MIN", "5"))
SEEN_FILE           = Path(os.environ.get("SEEN_FILE", "seen_tweets.json"))

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

# ── Twitter via syndication API (sin auth) ────────────────────────────────────
async def fetch_tweets() -> list[dict]:
    """
    Usa la API de syndication de Twitter que es pública y no requiere auth.
    """
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{TWITTER_USERNAME}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": f"https://twitter.com/{TWITTER_USERNAME}",
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            log.info(f"Syndication API: {resp.status_code}")
            if resp.status_code == 200:
                return parse_syndication(resp.text)
    except Exception as e:
        log.error(f"Error fetching tweets: {e}")
    return []

def parse_syndication(html: str) -> list[dict]:
    """Extrae tweets del HTML de syndication."""
    tweets = []
    # Buscar el JSON embebido en el HTML
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not match:
        log.warning("No se encontró __NEXT_DATA__ en syndication")
        return []
    try:
        data = json.loads(match.group(1))
        entries = (
            data.get("props", {})
                .get("pageProps", {})
                .get("timeline", {})
                .get("entries", [])
        )
        for entry in entries:
            tweet = entry.get("content", {}).get("tweet", {})
            if not tweet:
                continue
            tweet_id = tweet.get("id_str", "")
            full_text = tweet.get("full_text", tweet.get("text", ""))
            # Limpiar URLs de Twitter del texto
            full_text = re.sub(r"https://t\.co/\S+", "", full_text).strip()

            # Buscar media
            media_url = ""
            media_entities = tweet.get("entities", {}).get("media", [])
            extended = tweet.get("extended_entities", {}).get("media", [])
            all_media = extended or media_entities
            for m in all_media:
                if m.get("type") in ("photo", "video", "animated_gif"):
                    media_url = m.get("media_url_https", m.get("media_url", ""))
                    break

            tweets.append({
                "id": tweet_id,
                "text": full_text,
                "link": f"https://twitter.com/{TWITTER_USERNAME}/status/{tweet_id}",
                "media_url": media_url,
            })
    except Exception as e:
        log.error(f"Error parseando syndication: {e}")
    return tweets

# ── MLB Stats API ─────────────────────────────────────────────────────────────
MLB_API = "https://statsapi.mlb.com/api/v1"

def extract_player_name(text: str) -> str | None:
    match = re.match(r"^(.+?)\s+[-–]?\s*\w[\w\s]+\(\d+\)", text)
    if match:
        return match.group(1).strip()
    match2 = re.match(r"^(.+?)\s+\d{1,3}(?:st|nd|rd|th)\s+Home Run", text, re.IGNORECASE)
    if match2:
        return match2.group(1).strip()
    words = text.split()
    if len(words) >= 2:
        return f"{words[0]} {words[1]}"
    return None

async def get_todays_homeruns(client: httpx.AsyncClient) -> list[dict]:
    today = date.today().strftime("%Y-%m-%d")
    try:
        resp = await client.get(
            f"{MLB_API}/schedule",
            params={
                "sportId": 1,
                "date": today,
                "hydrate": "scoringplays",
            },
            timeout=15,
        )
        data = resp.json()
        game_pks = []
        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                game_pks.append(game.get("gamePk"))

        homeruns = []
        for gk in game_pks:
            try:
                gr = await client.get(f"{MLB_API}/game/{gk}/playByPlay", timeout=15)
                gdata = gr.json()
                for play in gdata.get("allPlays", []):
                    result = play.get("result", {})
                    if result.get("eventType") == "home_run":
                        matchup = play.get("matchup", {})
                        hit_data = {}
                        for ev in play.get("playEvents", []):
                            hd = ev.get("hitData", {})
                            if hd:
                                hit_data = hd
                        homeruns.append({
                            "batter_name": matchup.get("batter", {}).get("fullName", ""),
                            "pitcher_name": matchup.get("pitcher", {}).get("fullName", ""),
                            "pitcher_team": "",
                            "distance": hit_data.get("totalDistance", 0),
                            "exit_velocity": hit_data.get("launchSpeed", 0),
                            "launch_angle": hit_data.get("launchAngle", 0),
                            "description": result.get("description", ""),
                        })
            except Exception:
                pass
        return homeruns
    except Exception as e:
        log.warning(f"MLB API error: {e}")
        return []

async def find_hr_data(player_name: str) -> dict | None:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        homeruns = await get_todays_homeruns(client)
        name_lower = player_name.lower()
        for hr in homeruns:
            batter = hr["batter_name"].lower()
            if name_lower in batter or batter in name_lower:
                return hr
    return None

# ── Formatear mensaje ─────────────────────────────────────────────────────────
def escape_md(text: str) -> str:
    for c in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(c, f"\\{c}")
    return text

def format_message(tweet: dict, hr_data: dict | None) -> str:
    raw = tweet["text"].strip()
    title = escape_md(raw.split("\n")[0][:150] if "\n" in raw else raw[:150])

    if hr_data and hr_data.get("distance") and float(hr_data["distance"]) > 0:
        dist   = hr_data["distance"]
        vel    = hr_data["exit_velocity"]
        angle  = hr_data["launch_angle"]
        pitcher = escape_md(hr_data.get("pitcher_name", "N/A"))
        return (
            f"⚾ *{title}*\n\n"
            f"📏 Distance: {dist}ft\n"
            f"💥 Exit Velocity: {vel} MPH\n"
            f"📐 Launch Angle: {angle}°\n"
            f"🎯 Pitcher: {pitcher}"
        )
    else:
        lines = [escape_md(l.strip()) for l in raw.split("\n") if l.strip()]
        if len(lines) > 1:
            return f"⚾ *{lines[0]}*\n\n" + "\n".join(lines[1:])
        return f"⚾ *{escape_md(raw[:400])}*"

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
                log.info(f"Enviado con foto: {tweet['id']}")
                return
            except Exception as e:
                log.warning(f"Foto falló: {e}")
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )
        log.info(f"Enviado (texto): {tweet['id']}")
    except Exception as e:
        log.error(f"Error Telegram: {e}")

# ── Job principal ─────────────────────────────────────────────────────────────
async def check_and_post(bot: Bot):
    seen = load_seen()
    tweets = await fetch_tweets()

    if not tweets:
        log.info("Sin tweets nuevos.")
        return

    log.info(f"{len(tweets)} tweets encontrados.")
    nuevos = 0
    for tweet in reversed(tweets):
        if tweet["id"] in seen:
            continue
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
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Health-check en :{port}")

# ── Entrypoint ────────────────────────────────────────────────────────────────
async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    me = await bot.get_me()
    log.info(f"Bot conectado: @{me.username}")
    await health_server()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_and_post, "interval",
        minutes=CHECK_INTERVAL_MIN, args=[bot],
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    log.info(f"Revisando cada {CHECK_INTERVAL_MIN} min.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
