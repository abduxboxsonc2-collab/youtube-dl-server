import re
import os
import json
import time
import yt_dlp
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
COOKIEFILE = os.getenv("COOKIEFILE", "")          # /app/cookies.txt if you upload one
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0",
)
COMBINED_FORMAT_MATCH = r"\[([^\+]+=[^\+]+)\+.*\]"

# Public PO‑Token provider (optional – not strictly needed with cookies)
POT_PROVIDER_URL = os.getenv("POT_PROVIDER_URL", "https://yt-dlp-pot-provider.vercel.app")


class ErrorLogger:
    def __init__(self):
        self.errors = []
        self.warnings = []
    def debug(self, msg): pass
    def warning(self, msg): self.warnings.append(msg)
    def error(self, msg): self.errors.append(msg)


async def fetch_pot():
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{POT_PROVIDER_URL}/get")
            resp.raise_for_status()
            return resp.json()["token"]
    except Exception:
        return None

_pot_cache = {"token": None, "timestamp": 0}
async def get_pot():
    now = time.time()
    if _pot_cache["token"] and (now - _pot_cache["timestamp"]) < 300:
        return _pot_cache["token"]
    token = await fetch_pot()
    if token:
        _pot_cache["token"] = token
        _pot_cache["timestamp"] = now
    return token


# ---------- Health ----------
async def health(request):
    return JSONResponse({"status": "ok"})


# ---------- /info with multiple extraction attempts ----------
async def info(request):
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "errors": ["Missing url parameter"]})

    media_format = request.query_params.get("format", "")
    user_agent = request.query_params.get("user-agent", USER_AGENT)

    # format cleanup (unchanged)
    if re.search(COMBINED_FORMAT_MATCH, media_format):
        in_brackets = False
        i = len(media_format)
        while i > 0:
            i -= 1
            c = media_format[i]
            if c == "[":
                in_brackets = False
            elif c == "]":
                in_brackets = True
            elif in_brackets and c == "+":
                media_format = media_format[:i] + "][" + media_format[i + 1 :]

    # ---------- List of strategies to try (borrowed from your old Node.js code) ----------
    strategies = [
        # (format, player_client)
        ("best[height<=720]", "android"),
        ("best[height<=480]", "ios"),
        ("best",              "web"),
    ]

    last_errors = []
    all_warnings = []

    for fmt, client in strategies:
        logger = ErrorLogger()
        opts = {
            "quiet": True,
            "noplaylist": True,
            "logger": logger,
            "no_color": True,
            "http_headers": {"User-Agent": user_agent},
            "extractor_args": {"youtube": {"player_client": [client]}},
        }

        # If a cookie file exists, use it – this is the most reliable fix
        if os.path.exists(COOKIEFILE) and os.path.getsize(COOKIEFILE) > 0:
            opts["cookiefile"] = COOKIEFILE
        else:
            # Without cookies, try the PO‑Token if available
            pot = await get_pot()
            if pot:
                opts["use_pot"] = True
                opts["pot_url"] = POT_PROVIDER_URL
                opts["pot_token"] = pot

        if media_format:
            opts["format"] = media_format
        else:
            opts["format"] = fmt

        try:
            with yt_dlp.YoutubeDL(opts) as ytdl:
                data = ytdl.extract_info(url, download=False)
        except Exception as e:
            last_errors.append(f"[{client}/{fmt}] {e}")
            all_warnings.extend(logger.warnings)
            continue

        if not data:
            last_errors.append(f"[{client}/{fmt}] No data returned")
            continue

        entries = [data] if "entries" not in data else data["entries"]
        valid_entries = []
        for entry in entries:
            if "url" in entry and entry["url"]:
                valid_entries.append(entry)
            else:
                logger.warning(f"No direct URL for {entry.get('title', 'unknown')}")

        if valid_entries:
            return JSONResponse({
                "success": True,
                "errors": logger.errors,
                "warnings": logger.warnings,
                "data": valid_entries,
            })
        else:
            last_errors.append(f"[{client}/{fmt}] No playable URL found")
            all_warnings.extend(logger.warnings)

    # All strategies failed
    return JSONResponse({
        "success": False,
        "errors": last_errors,
        "warnings": all_warnings,
        "data": [],
    })


# ---------- /search (unchanged) ----------
async def search_handler(request):
    q = request.query_params.get("q")
    page_token = request.query_params.get("pageToken")
    if not q:
        return JSONResponse({"error": "Missing q parameter"}, status_code=400)

    params = {
        "part": "snippet",
        "maxResults": 20,
        "q": q,
        "type": "video",
        "key": YOUTUBE_API_KEY,
    }
    if page_token:
        params["pageToken"] = page_token

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/search", params=params
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return JSONResponse({"error": f"Search failed: {e}"}, status_code=500)

    items = data.get("items", [])
    video_ids = [i["id"]["videoId"] for i in items]

    durations = {}
    if video_ids:
        try:
            dur_params = {
                "part": "contentDetails",
                "id": ",".join(video_ids),
                "key": YOUTUBE_API_KEY,
            }
            async with httpx.AsyncClient() as client:
                dur_resp = await client.get(
                    "https://www.googleapis.com/youtube/v3/videos", params=dur_params
                )
                dur_resp.raise_for_status()
                dur_data = dur_resp.json()
            for item in dur_data.get("items", []):
                durations[item["id"]] = item["contentDetails"]["duration"]
        except Exception:
            pass

    videos = []
    for item in items:
        snippet = item["snippet"]
        video_id = item["id"]["videoId"]
        videos.append({
            "videoId": video_id,
            "title": snippet["title"],
            "author": snippet["channelTitle"],
            "thumbnail": (
                snippet["thumbnails"]["high"]["url"]
                if "high" in snippet["thumbnails"]
                else snippet["thumbnails"]["default"]["url"]
            ),
            "duration": durations.get(video_id, "Unknown"),
        })

    return JSONResponse({
        "videos": videos,
        "nextPageToken": data.get("nextPageToken"),
    })


routes = [
    Route("/", endpoint=health, methods=["GET"]),
    Route("/info", endpoint=info, methods=["GET"]),
    Route("/search", endpoint=search_handler, methods=["GET"]),
]

app = Starlette(routes=routes)
